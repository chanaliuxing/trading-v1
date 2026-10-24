-- Idempotent repair for older core_order_event deployments that predate
-- reconnect-safe order event identity columns.

ALTER TABLE core.core_order_event
    ADD COLUMN IF NOT EXISTS source_event_id text NULL;

ALTER TABLE core.core_order_event
    ADD COLUMN IF NOT EXISTS ib_connection_epoch integer NULL;

CREATE INDEX IF NOT EXISTS core_order_event_source_event_idx
    ON core.core_order_event (source_event_id);

CREATE INDEX IF NOT EXISTS core_order_event_epoch_idx
    ON core.core_order_event (ib_connection_epoch);
