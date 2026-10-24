-- lake.bbo_1s definition

CREATE TABLE lake.bbo_1s
(

    `symbol` String,

    `ts` DateTime,

    `bid` Float64,

    `ask` Float64,

    `bid_size` UInt32,

    `ask_size` UInt32,

    `ver` UInt64,

    `d` Date DEFAULT toDate(ts)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY d
ORDER BY (symbol,
 ts)
TTL d + toIntervalDay(90)
SETTINGS index_granularity = 8192;


-- lake.bbo_1s_features definition

CREATE TABLE lake.bbo_1s_features
(

    `symbol` String,

    `ts` DateTime,

    `mid` Nullable(Float64),

    `spread` Nullable(Float64),

    `spread_bps` Nullable(Float64),

    `imbalance` Nullable(Float64),

    `microprice` Nullable(Float64),

    `is_valid` UInt8,

    `ver` UInt64,

    `d` Date DEFAULT toDate(ts)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY d
ORDER BY (symbol,
 ts)
TTL d + toIntervalDay(180)
SETTINGS index_granularity = 8192;


-- lake.market_cost_intraday_minute definition

CREATE TABLE lake.market_cost_intraday_minute
(

    `symbol` String,

    `d` Date,

    `minute_of_day` UInt16,

    `p95_spread_bps` Nullable(Float64),

    `p50_spread_bps` Nullable(Float64),

    `avg_spread_bps` Nullable(Float64),

    `samples` UInt32,

    `updated_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY d
ORDER BY (symbol,
 d,
 minute_of_day)
TTL d + toIntervalDay(365)
SETTINGS index_granularity = 8192;


-- lake.quotes_l1 definition

CREATE TABLE lake.quotes_l1
(

    `symbol` String,

    `ts` DateTime64(6),

    `bid` Float64,

    `ask` Float64,

    `bid_size` UInt32,

    `ask_size` UInt32,

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `ver` UInt64,

    `d` Date DEFAULT toDate(ts)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY d
ORDER BY (symbol,
 ts)
TTL d + toIntervalDay(30)
SETTINGS index_granularity = 8192;


-- lake.ticks_trades definition

CREATE TABLE lake.ticks_trades
(

    `symbol` String,

    `ts` DateTime64(6),

    `price` Float64,

    `size` UInt32,

    `exchange` LowCardinality(String) DEFAULT 'SMART',

    `is_rth` UInt8 DEFAULT 1,

    `source` LowCardinality(String) DEFAULT 'IBKR',

    `ver` UInt64,

    `d` Date DEFAULT toDate(ts)
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY d
ORDER BY (symbol,
 ts)
TTL d + toIntervalDay(30)
SETTINGS index_granularity = 8192;