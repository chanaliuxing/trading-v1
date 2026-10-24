-- trading.bars_1d_ibkr definition

CREATE TABLE trading.bars_1d_ibkr
(

    `symbol` String,

    `bar_sec` UInt32 DEFAULT 86400,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMM(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(1825)
SETTINGS index_granularity = 8192;


-- trading.bars_1h definition

CREATE TABLE trading.bars_1h
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 3600,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(720)
SETTINGS index_granularity = 8192;


-- trading.bars_1m definition

CREATE TABLE trading.bars_1m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 60,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(180)
SETTINGS index_granularity = 8192;


-- trading.bars_1m_ibkr definition

CREATE TABLE trading.bars_1m_ibkr
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 60,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(180)
SETTINGS index_granularity = 8192;


-- trading.bars_2h_ibkr definition

CREATE TABLE trading.bars_2h_ibkr
(

    `symbol` String,

    `bar_sec` UInt32 DEFAULT 7200,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(730)
SETTINGS index_granularity = 8192;


-- trading.bars_2m definition

CREATE TABLE trading.bars_2m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 120,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.bars_3h_ibkr definition

CREATE TABLE trading.bars_3h_ibkr
(

    `symbol` String,

    `bar_sec` UInt32 DEFAULT 10800,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(730)
SETTINGS index_granularity = 8192;


-- trading.bars_3m definition

CREATE TABLE trading.bars_3m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 180,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_4h_ibkr definition

CREATE TABLE trading.bars_4h_ibkr
(

    `symbol` String,

    `bar_sec` UInt32 DEFAULT 14400,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(730)
SETTINGS index_granularity = 8192;


-- trading.bars_5m definition

CREATE TABLE trading.bars_5m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 300,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_5m_ibkr definition

CREATE TABLE trading.bars_5m_ibkr
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 300,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_5s definition

CREATE TABLE trading.bars_5s
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 5,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.bars_10m definition

CREATE TABLE trading.bars_10m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 600,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_10m_ibkr definition

CREATE TABLE trading.bars_10m_ibkr
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 600,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.bars_10s definition

CREATE TABLE trading.bars_10s
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 10,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.bars_15m definition

CREATE TABLE trading.bars_15m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 900,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_15m_ibkr definition

CREATE TABLE trading.bars_15m_ibkr
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 900,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_15s definition

CREATE TABLE trading.bars_15s
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 15,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.bars_30m definition

CREATE TABLE trading.bars_30m
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 1800,

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

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
SETTINGS index_granularity = 8192;


-- trading.bars_30m_ibkr definition

CREATE TABLE trading.bars_30m_ibkr
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 1800,

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

    `minute_of_day` UInt16,

    `ver` UInt64,

    `is_backfill` UInt8,

    `created_at` DateTime,

    `ingest_ts` DateTime64(3)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(180)
SETTINGS index_granularity = 8192;


-- trading.bars_30s definition

CREATE TABLE trading.bars_30s
(

    `symbol` String,

    `bar_sec` UInt16 DEFAULT 5,

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

    `what_to_show` LowCardinality(String) DEFAULT 'TRADES',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `currency` LowCardinality(String) DEFAULT 'USD',

    `tz` LowCardinality(String) DEFAULT 'America/New_York',

    `d` Date DEFAULT toDate(ts_start),

    `minute_of_day` UInt16 DEFAULT (toHour(ts_start) * 60) + toMinute(ts_start),

    `ver` UInt64,

    `is_backfill` UInt8 DEFAULT 0,

    `created_at` DateTime DEFAULT now(),

    `ingest_ts` DateTime64(3) DEFAULT now64(3),

    `ohlc4` Float64 ALIAS (((open + high) + low) + close) / 4,

    `hlc3` Float64 ALIAS ((high + low) + close) / 3,

    `range` Float64 ALIAS high - low,

    `body` Float64 ALIAS close - open,

    `notional` Float64 ALIAS volume * close
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts_start)
ORDER BY (symbol,
 ts_start)
TTL ts_start + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.dim_symbol definition

CREATE TABLE trading.dim_symbol
(

    `symbol` LowCardinality(String),

    `sector` LowCardinality(String),

    `industry` LowCardinality(String)
)
ENGINE = MergeTree
PRIMARY KEY symbol
ORDER BY symbol
SETTINGS index_granularity = 8192;


-- trading.exec_events definition

CREATE TABLE trading.exec_events
(

    `event_id` String,

    `ts` DateTime,

    `strategy_id` String,

    `symbol` String,

    `order_id` String,

    `event_type` Enum8('SUBMITTED' = 1,
 'PARTIAL' = 2,
 'FILLED' = 3,
 'CANCELLED' = 4,
 'REJECTED' = 5),

    `qty` Float64,

    `price` Nullable(Float64),

    `broker_order_id` Nullable(String),

    `details` String,

    `created_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (strategy_id,
 symbol,
 ts,
 event_id)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_1d definition

CREATE TABLE trading.features_1d
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_1m definition

CREATE TABLE trading.features_1m
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_2h definition

CREATE TABLE trading.features_2h
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_3h definition

CREATE TABLE trading.features_3h
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_4h definition

CREATE TABLE trading.features_4h
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_5m definition

CREATE TABLE trading.features_5m
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 300,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_5s definition

CREATE TABLE trading.features_5s
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64) COMMENT '1-period return',

    `ret_5` Nullable(Float64) COMMENT '5-period return',

    `log_ret` Nullable(Float64) COMMENT 'Log return',

    `roc_5` Nullable(Float64) COMMENT 'Rate of change 5-period',

    `roc_10` Nullable(Float64) COMMENT 'Rate of change 10-period',

    `sma_5` Nullable(Float64) COMMENT 'Simple moving average 5-period',

    `sma_20` Nullable(Float64) COMMENT 'Simple moving average 20-period',

    `sma_60` Nullable(Float64) COMMENT 'Simple moving average 60-period',

    `std_20` Nullable(Float64) COMMENT 'Standard deviation 20-period',

    `std_60` Nullable(Float64) COMMENT 'Standard deviation 60-period',

    `z_price_60` Nullable(Float64) COMMENT 'Price z-score 60-period',

    `bb_mid_20` Nullable(Float64) COMMENT 'Bollinger Band middle (20-period SMA)',

    `bb_up_20` Nullable(Float64) COMMENT 'Bollinger Band upper (mid + 2*std)',

    `bb_dn_20` Nullable(Float64) COMMENT 'Bollinger Band lower (mid - 2*std)',

    `bb_bw_20` Nullable(Float64) COMMENT 'Bollinger Band width ratio',

    `bb_pct_20` Nullable(Float64) COMMENT 'Bollinger %B (position within bands)',

    `stoch_k_14` Nullable(Float64) COMMENT 'Stochastic %K (14-period)',

    `stoch_d_3` Nullable(Float64) COMMENT 'Stochastic %D (3-period SMA of %K)',

    `rsi_14` Nullable(Float64) COMMENT 'Relative Strength Index (14-period)',

    `rv_20` Nullable(Float64) COMMENT 'Realized volatility (20-period)',

    `parkinson_20` Nullable(Float64) COMMENT 'Parkinson volatility estimator (20-period)',

    `garman_klass_20` Nullable(Float64) COMMENT 'Garman-Klass volatility estimator (20-period)',

    `donchian_hi_20` Nullable(Float64) COMMENT 'Donchian channel high (20-period)',

    `donchian_lo_20` Nullable(Float64) COMMENT 'Donchian channel low (20-period)',

    `di_plus_14` Nullable(Float64) COMMENT 'Directional Indicator +DI (14-period)',

    `di_minus_14` Nullable(Float64) COMMENT 'Directional Indicator -DI (14-period)',

    `dx_14` Nullable(Float64) COMMENT 'Directional Movement Index (14-period)',

    `adx_14` Nullable(Float64) COMMENT 'Average Directional Index (14-period)',

    `vol_avg_20` Nullable(Float64) COMMENT 'Volume average (20-period)',

    `rvol_roll_20` Nullable(Float64) COMMENT 'Relative volume: current / 20-period avg',

    `rvol_tod_20d` Nullable(Float64) COMMENT 'Relative volume: current / same time-of-day avg (20 days)',

    `cmf_20` Nullable(Float64) COMMENT 'Chaikin Money Flow (20-period)',

    `mfi_14` Nullable(Float64) COMMENT 'Money Flow Index (14-period)',

    `obv_60` Nullable(Float64) COMMENT 'On-Balance Volume (60-period window)',

    `vwap_day` Nullable(Float64) COMMENT 'Intraday VWAP (session cumulative)',

    `vwap_dev_bps` Nullable(Float64) COMMENT 'Deviation from VWAP in basis points',

    `vwap_slope_20` Nullable(Float64) COMMENT 'VWAP trend strength (20-period correlation)',

    `micro_mid` Nullable(Float64) COMMENT 'Microprice mid (from L1)',

    `close_to_mid_bps` Nullable(Float64) COMMENT 'Close price deviation from mid in bps',

    `body` Nullable(Float64) COMMENT 'Candlestick body (close - open)',

    `upper_wick` Nullable(Float64) COMMENT 'Upper wick (high - max(open,
\nclose))',

    `lower_wick` Nullable(Float64) COMMENT 'Lower wick (min(open,
\nclose) - low)',

    `range` Nullable(Float64) COMMENT 'High-low range',

    `range_pct` Nullable(Float64) COMMENT 'Range as percentage of close',

    `bar_sec` UInt16 COMMENT 'Bar size in seconds',

    `minute_of_day` UInt16 COMMENT 'Minute of day (0-1440)',

    `open` Nullable(Float64) COMMENT 'Open price',

    `high` Nullable(Float64) COMMENT 'High price',

    `low` Nullable(Float64) COMMENT 'Low price',

    `vwap_bar` Nullable(Float64) COMMENT 'Bar VWAP',

    `trades` UInt32 COMMENT 'Number of trades',

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.features_10m definition

CREATE TABLE trading.features_10m
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 600,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_10s definition

CREATE TABLE trading.features_10s
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64) COMMENT '1-period return',

    `ret_5` Nullable(Float64) COMMENT '5-period return',

    `log_ret` Nullable(Float64) COMMENT 'Log return',

    `roc_5` Nullable(Float64) COMMENT 'Rate of change 5-period',

    `roc_10` Nullable(Float64) COMMENT 'Rate of change 10-period',

    `sma_5` Nullable(Float64) COMMENT 'Simple moving average 5-period',

    `sma_20` Nullable(Float64) COMMENT 'Simple moving average 20-period',

    `sma_60` Nullable(Float64) COMMENT 'Simple moving average 60-period',

    `std_20` Nullable(Float64) COMMENT 'Standard deviation 20-period',

    `std_60` Nullable(Float64) COMMENT 'Standard deviation 60-period',

    `z_price_60` Nullable(Float64) COMMENT 'Price z-score 60-period',

    `bb_mid_20` Nullable(Float64) COMMENT 'Bollinger Band middle (20-period SMA)',

    `bb_up_20` Nullable(Float64) COMMENT 'Bollinger Band upper (mid + 2*std)',

    `bb_dn_20` Nullable(Float64) COMMENT 'Bollinger Band lower (mid - 2*std)',

    `bb_bw_20` Nullable(Float64) COMMENT 'Bollinger Band width ratio',

    `bb_pct_20` Nullable(Float64) COMMENT 'Bollinger %B (position within bands)',

    `stoch_k_14` Nullable(Float64) COMMENT 'Stochastic %K (14-period)',

    `stoch_d_3` Nullable(Float64) COMMENT 'Stochastic %D (3-period SMA of %K)',

    `rsi_14` Nullable(Float64) COMMENT 'Relative Strength Index (14-period)',

    `rv_20` Nullable(Float64) COMMENT 'Realized volatility (20-period)',

    `parkinson_20` Nullable(Float64) COMMENT 'Parkinson volatility estimator (20-period)',

    `garman_klass_20` Nullable(Float64) COMMENT 'Garman-Klass volatility estimator (20-period)',

    `donchian_hi_20` Nullable(Float64) COMMENT 'Donchian channel high (20-period)',

    `donchian_lo_20` Nullable(Float64) COMMENT 'Donchian channel low (20-period)',

    `di_plus_14` Nullable(Float64) COMMENT 'Directional Indicator +DI (14-period)',

    `di_minus_14` Nullable(Float64) COMMENT 'Directional Indicator -DI (14-period)',

    `dx_14` Nullable(Float64) COMMENT 'Directional Movement Index (14-period)',

    `adx_14` Nullable(Float64) COMMENT 'Average Directional Index (14-period)',

    `vol_avg_20` Nullable(Float64) COMMENT 'Volume average (20-period)',

    `rvol_roll_20` Nullable(Float64) COMMENT 'Relative volume: current / 20-period avg',

    `rvol_tod_20d` Nullable(Float64) COMMENT 'Relative volume: current / same time-of-day avg (20 days)',

    `cmf_20` Nullable(Float64) COMMENT 'Chaikin Money Flow (20-period)',

    `mfi_14` Nullable(Float64) COMMENT 'Money Flow Index (14-period)',

    `obv_60` Nullable(Float64) COMMENT 'On-Balance Volume (60-period window)',

    `vwap_day` Nullable(Float64) COMMENT 'Intraday VWAP (session cumulative)',

    `vwap_dev_bps` Nullable(Float64) COMMENT 'Deviation from VWAP in basis points',

    `vwap_slope_20` Nullable(Float64) COMMENT 'VWAP trend strength (20-period correlation)',

    `micro_mid` Nullable(Float64) COMMENT 'Microprice mid (from L1)',

    `close_to_mid_bps` Nullable(Float64) COMMENT 'Close price deviation from mid in bps',

    `body` Nullable(Float64) COMMENT 'Candlestick body (close - open)',

    `upper_wick` Nullable(Float64) COMMENT 'Upper wick (high - max(open,
\nclose))',

    `lower_wick` Nullable(Float64) COMMENT 'Lower wick (min(open,
\nclose) - low)',

    `range` Nullable(Float64) COMMENT 'High-low range',

    `range_pct` Nullable(Float64) COMMENT 'Range as percentage of close',

    `bar_sec` UInt16 COMMENT 'Bar size in seconds',

    `minute_of_day` UInt16 COMMENT 'Minute of day (0-1440)',

    `open` Nullable(Float64) COMMENT 'Open price',

    `high` Nullable(Float64) COMMENT 'High price',

    `low` Nullable(Float64) COMMENT 'Low price',

    `vwap_bar` Nullable(Float64) COMMENT 'Bar VWAP',

    `trades` UInt32 COMMENT 'Number of trades',

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.features_15m definition

CREATE TABLE trading.features_15m
(

    `symbol` String,

    `ts` DateTime,

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `bar_sec` UInt16,

    `minute_of_day` UInt16,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `vwap_bar` Nullable(Float64),

    `trades` UInt64,

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_30m definition

CREATE TABLE trading.features_30m
(

    `symbol` String,

    `ts` DateTime,

    `close` Float64,

    `vwap` Float64,

    `atr_14` Nullable(Float64),

    `atr_5` Nullable(Float64),

    `ema_10` Nullable(Float64),

    `ema_20` Nullable(Float64),

    `ema_50` Nullable(Float64),

    `volume` Float64,

    `vol_ema_20` Nullable(Float64),

    `vol_spike` Nullable(Float64),

    `z_price_20` Nullable(Float64),

    `z_vol_20` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `slope_20` Nullable(Float64),

    `r2_20` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `ret_1` Nullable(Float64),

    `ret_5` Nullable(Float64),

    `log_ret` Nullable(Float64),

    `roc_5` Nullable(Float64),

    `roc_10` Nullable(Float64),

    `sma_5` Nullable(Float64),

    `sma_20` Nullable(Float64),

    `sma_60` Nullable(Float64),

    `std_20` Nullable(Float64),

    `std_60` Nullable(Float64),

    `z_price_60` Nullable(Float64),

    `bb_mid_20` Nullable(Float64),

    `bb_up_20` Nullable(Float64),

    `bb_dn_20` Nullable(Float64),

    `bb_bw_20` Nullable(Float64),

    `bb_pct_20` Nullable(Float64),

    `stoch_k_14` Nullable(Float64),

    `stoch_d_3` Nullable(Float64),

    `rsi_14` Nullable(Float64),

    `rv_20` Nullable(Float64),

    `parkinson_20` Nullable(Float64),

    `garman_klass_20` Nullable(Float64),

    `donchian_hi_20` Nullable(Float64),

    `donchian_lo_20` Nullable(Float64),

    `di_plus_14` Nullable(Float64),

    `di_minus_14` Nullable(Float64),

    `dx_14` Nullable(Float64),

    `adx_14` Nullable(Float64),

    `vol_avg_20` Nullable(Float64),

    `rvol_roll_20` Nullable(Float64),

    `rvol_tod_20d` Nullable(Float64),

    `cmf_20` Nullable(Float64),

    `mfi_14` Nullable(Float64),

    `obv_60` Nullable(Float64),

    `vwap_day` Nullable(Float64),

    `vwap_dev_bps` Nullable(Float64),

    `vwap_slope_20` Nullable(Float64),

    `micro_mid` Nullable(Float64),

    `close_to_mid_bps` Nullable(Float64),

    `body` Nullable(Float64),

    `upper_wick` Nullable(Float64),

    `lower_wick` Nullable(Float64),

    `range` Nullable(Float64),

    `range_pct` Nullable(Float64),

    `bar_sec` UInt16 DEFAULT 60,

    `minute_of_day` UInt16,

    `open` Nullable(Float64),

    `high` Nullable(Float64),

    `low` Nullable(Float64),

    `vwap_bar` Nullable(Float64),

    `trades` UInt32,

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64),

    `ema_9` Nullable(Float64) COMMENT 'EMA 9-period',

    `ema_21` Nullable(Float64) COMMENT 'EMA 21-period',

    `ema_cross` Nullable(Float64) COMMENT 'EMA cross (ema_9 - ema_21)',

    `vix_diff` Nullable(Float64) COMMENT 'VIX - VIX_SMA_20',

    `atr_20` Nullable(Float64) COMMENT 'ATR 20-period',

    `spy_ret_1` Nullable(Float64) COMMENT 'SPY 1-period return',

    `spy_ret_5` Nullable(Float64) COMMENT 'SPY 5-period return',

    `qqq_ret_1` Nullable(Float64) COMMENT 'QQQ 1-period return',

    `qqq_ret_5` Nullable(Float64) COMMENT 'QQQ 5-period return',

    `beta_spy_20` Nullable(Float64) COMMENT 'Rolling beta vs SPY (20-period)',

    `beta_spy_60` Nullable(Float64) COMMENT 'Rolling beta vs SPY (60-period)',

    `corr_spy_20` Nullable(Float64) COMMENT 'Rolling correlation with SPY (20-period)',

    `corr_spy_60` Nullable(Float64) COMMENT 'Rolling correlation with SPY (60-period)',

    `corr_qqq_20` Nullable(Float64) COMMENT 'Rolling correlation with QQQ (20-period)',

    `tick_imbalance` Nullable(Float64) COMMENT 'Trade tick imbalance ratio',

    `trade_count_ratio` Nullable(Float64) COMMENT 'Trade count / rolling avg',

    `avg_trade_size` Nullable(Float64) COMMENT 'Average trade size (volume/trades)',

    `price_impact_5m` Nullable(Float64) COMMENT '5-minute price impact',

    `zscore_close_20` Nullable(Float64) COMMENT 'Close z-score (20-period)',

    `zscore_close_60` Nullable(Float64) COMMENT 'Close z-score (60-period)',

    `zscore_volume_20` Nullable(Float64) COMMENT 'Volume z-score (20-period)',

    `zscore_volume_60` Nullable(Float64) COMMENT 'Volume z-score (60-period)',

    `skew_20` Nullable(Float64) COMMENT 'Return skewness (20-period)',

    `skew_60` Nullable(Float64) COMMENT 'Return skewness (60-period)',

    `kurt_20` Nullable(Float64) COMMENT 'Return excess kurtosis (20-period)',

    `kurt_60` Nullable(Float64) COMMENT 'Return excess kurtosis (60-period)',

    `autocorr_ret_1` Nullable(Float64) COMMENT 'Return autocorrelation lag-1',

    `autocorr_ret_5` Nullable(Float64) COMMENT 'Return autocorrelation lag-5',

    `hurst_20` Nullable(Float64) COMMENT 'Hurst exponent (20-period R/S)',

    `time_since_open` Nullable(UInt16) COMMENT 'Minutes since market open (9:30 ET)',

    `day_of_week` Nullable(UInt8) COMMENT 'Day of week (0=Mon,
\n 4=Fri)',

    `hour_of_day` Nullable(UInt8) COMMENT 'Hour of day (0-23 ET)',

    `is_first_30min` Nullable(UInt8) COMMENT 'Flag: first 30min of session',

    `is_last_30min` Nullable(UInt8) COMMENT 'Flag: last 30min of session',

    `kama_10` Nullable(Float64) COMMENT 'Kaufman Adaptive MA (10-period)',

    `entropy_20` Nullable(Float64) COMMENT 'Shannon entropy of returns (20-period)'
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- trading.features_60m definition

CREATE TABLE trading.features_60m
(

    `symbol` String,

    `ts` DateTime,

    `open` Float64,

    `high` Float64,

    `low` Float64,

    `close` Float64,

    `volume` Float64,

    `trades` UInt64,

    `vwap` Nullable(Float64),

    `ver` UInt64,

    `created_at` DateTime DEFAULT now(),

    `atr14` Nullable(Float64),

    `rsi14` Nullable(Float64),

    `ema12` Nullable(Float64),

    `ema26` Nullable(Float64),

    `macd` Nullable(Float64),

    `macd_signal` Nullable(Float64),

    `adx14` Nullable(Float64),

    `vix` Nullable(Float64),

    `vix_sma_20` Nullable(Float64),

    `vix_z_20` Nullable(Float64),

    `vix_chg_1m` Nullable(Float64),

    `vix_chg_5m` Nullable(Float64),

    `vix_rsi_14` Nullable(Float64)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol,
 ts)
SETTINGS index_granularity = 8192;


-- trading.fills_live definition

CREATE TABLE trading.fills_live
(

    `ts` DateTime64(3),

    `strategy_id` LowCardinality(String),

    `symbol` LowCardinality(String),

    `side` Enum8('SELL' = -1,
 'BUY' = 1),

    `qty` Int64,

    `price` Float64,

    `fee` Float64 DEFAULT 0,

    `order_id` String,

    `decision_id` String,

    `param_hash` FixedString(10),

    `liquidity` LowCardinality(String) DEFAULT 'UNK',

    `fill_uid` FixedString(20),

    `ver` UInt64,

    `ingest_ts` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toDate(ts)
ORDER BY (strategy_id,
 symbol,
 ts,
 order_id,
 fill_uid)
SETTINGS index_granularity = 4096;


-- trading.pnl_ckpt definition

CREATE TABLE trading.pnl_ckpt
(

    `strategy_id` LowCardinality(String),

    `symbol` LowCardinality(String),

    `ts_ckpt` DateTime64(3),

    `pos` Int64,

    `avg` Float64,

    `cash` Float64,

    `ver` UInt64
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toDate(ts_ckpt)
ORDER BY (strategy_id,
 symbol,
 ts_ckpt)
SETTINGS index_granularity = 8192;


-- trading.pnl_day definition

CREATE TABLE trading.pnl_day
(

    `d` Date,

    `strategy_id` LowCardinality(String),

    `symbol` LowCardinality(String),

    `sector` LowCardinality(String),

    `traded_shares` UInt64,

    `traded_notional` Float64,

    `realized_pnl` Float64,

    `fees` Float64,

    `upl_close` Float64,

    `nav_close` Float64,

    `cap_used_close` Float64,

    `mkt_volume` Float64,

    `participation` Float64
)
ENGINE = SummingMergeTree
PARTITION BY d
ORDER BY (strategy_id,
 symbol,
 d)
SETTINGS index_granularity = 4096;


-- trading.pnl_fills definition

CREATE TABLE trading.pnl_fills
(

    `ts` DateTime64(3),

    `strategy_id` LowCardinality(String),

    `symbol` LowCardinality(String),

    `side` Int8,

    `qty` Int64,

    `price` Float64,

    `fee` Float64,

    `pos_qty_after` Int64,

    `avg_cost_after` Float64,

    `realized_pnl` Float64,

    `cash_after` Float64,

    `order_id` String,

    `decision_id` String,

    `param_hash` FixedString(10),

    `fill_uid` FixedString(20),

    `ver` UInt64,

    `ingest_ts` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toDate(ts)
ORDER BY (strategy_id,
 symbol,
 ts,
 fill_uid)
SETTINGS index_granularity = 4096;


-- trading.pnl_pos_5s definition

CREATE TABLE trading.pnl_pos_5s
(

    `ts_end` DateTime,

    `strategy_id` LowCardinality(String),

    `symbol` LowCardinality(String),

    `pos_qty` Int64,

    `avg_cost` Float64,

    `mark_px` Float64,

    `mtm` Float64,

    `upl` Float64,

    `ver` UInt64,

    `ingest_ts` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toDate(ts_end)
ORDER BY (strategy_id,
 symbol,
 ts_end)
SETTINGS index_granularity = 4096;


-- trading.portfolio_snapshots definition

CREATE TABLE trading.portfolio_snapshots
(

    `snapshot_id` String,

    `ts` DateTime,

    `strategy_id` String,

    `cash` Float64,

    `equity` Float64,

    `nav` Float64,

    `positions_json` String,

    `created_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (strategy_id,
 ts,
 snapshot_id)
TTL ts + toIntervalDay(180)
SETTINGS index_granularity = 8192;


-- trading.refs_vix_ticks definition

CREATE TABLE trading.refs_vix_ticks
(

    `ts` DateTime,

    `vix` Float64,

    `src` LowCardinality(String) DEFAULT 'ibkr'
)
ENGINE = MergeTree
ORDER BY ts
SETTINGS index_granularity = 8192;


-- trading.ticks_raw definition

CREATE TABLE trading.ticks_raw
(

    `msg_id` String,

    `source` String,

    `symbol` String,

    `ts_ns` UInt64,

    `type` Enum8('trade' = 1,
 'quote' = 2),

    `price` Nullable(Float64),

    `size` Nullable(Float64),

    `bid` Nullable(Float64),

    `ask` Nullable(Float64),

    `bid_size` Nullable(Float64),

    `ask_size` Nullable(Float64),

    `exchange` Nullable(String),

    `seq` Nullable(UInt64),

    `ingestion_ts` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(ingestion_ts)
PARTITION BY toYYYYMMDD(toDateTime(ts_ns / 1000000000))
ORDER BY (symbol,
 ts_ns,
 type,
 msg_id)
TTL toDateTime(ts_ns / 1000000000) + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- trading.trade_pnl definition

CREATE TABLE trading.trade_pnl
(

    `trade_id` String,

    `ts` DateTime,

    `strategy_id` String,

    `symbol` String,

    `side` Enum8('BUY' = 1,
 'SELL' = 2),

    `qty` Float64,

    `price` Float64,

    `commission` Float64,

    `realized_pnl` Nullable(Float64),

    `avg_cost` Nullable(Float64),

    `created_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (strategy_id,
 symbol,
 ts,
 trade_id)
TTL ts + toIntervalDay(365)
SETTINGS index_granularity = 8192;