-- core_order_event immutable audit/event table for traceability

CREATE TABLE IF NOT EXISTS core.core_order_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL,
    event_ts timestamptz NOT NULL DEFAULT now(),

    trace_id text NOT NULL,
    causation_id text NULL,
    source_event_id text NULL,

    strategy_id text NOT NULL,
    strategy_run_id text NOT NULL,
    trigger_id text NULL,
    decision_id text NOT NULL,
    intent_id text NULL,

    order_id uuid NULL,
    broker_order_id text NULL,
    parent_order_id uuid NULL,
    ib_connection_epoch integer NULL,
    symbol text NOT NULL,

    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS core_order_event_ts_idx
    ON core.core_order_event (event_ts DESC);

CREATE INDEX IF NOT EXISTS core_order_event_strategy_ts_idx
    ON core.core_order_event (strategy_id, event_ts DESC);

CREATE INDEX IF NOT EXISTS core_order_event_trace_idx
    ON core.core_order_event (trace_id);

CREATE INDEX IF NOT EXISTS core_order_event_decision_idx
    ON core.core_order_event (decision_id);

CREATE INDEX IF NOT EXISTS core_order_event_order_idx
    ON core.core_order_event (order_id);
