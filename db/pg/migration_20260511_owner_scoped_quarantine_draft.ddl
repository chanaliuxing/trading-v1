-- Draft only: owner-aware quarantine scope for OMS strategy isolation.
-- Nullable and non-destructive so unit tests and existing deployments do not
-- require an immediate data backfill.

ALTER TABLE IF EXISTS core.core_order_quarantine
    ADD COLUMN IF NOT EXISTS quarantine_scope text NULL,
    ADD COLUMN IF NOT EXISTS account_id text NULL,
    ADD COLUMN IF NOT EXISTS strategy_id text NULL,
    ADD COLUMN IF NOT EXISTS strategy_instance_id text NULL,
    ADD COLUMN IF NOT EXISTS intent_id text NULL,
    ADD COLUMN IF NOT EXISTS client_order_id text NULL,
    ADD COLUMN IF NOT EXISTS owner_key_hash text NULL,
    ADD COLUMN IF NOT EXISTS order_role text NULL,
    ADD COLUMN IF NOT EXISTS broker_order_id text NULL,
    ADD COLUMN IF NOT EXISTS ib_perm_id bigint NULL,
    ADD COLUMN IF NOT EXISTS source_event_id text NULL,
    ADD COLUMN IF NOT EXISTS ib_connection_epoch integer NULL,
    ADD COLUMN IF NOT EXISTS order_owner_key jsonb NULL;

ALTER TABLE IF EXISTS core.core_order_quarantine
    ADD CONSTRAINT core_order_quarantine_scope_ck
    CHECK (
        quarantine_scope IS NULL OR quarantine_scope IN (
            'GLOBAL',
            'ACCOUNT',
            'ACCOUNT_SYMBOL',
            'STRATEGY',
            'STRATEGY_INSTANCE',
            'ORDER_OWNER',
            'BROKER_ORDER'
        )
    ) NOT VALID;

CREATE INDEX IF NOT EXISTS core_order_quarantine_account_symbol_status_idx
    ON core.core_order_quarantine (account_id, symbol, status);

CREATE INDEX IF NOT EXISTS core_order_quarantine_account_strategy_instance_status_idx
    ON core.core_order_quarantine (account_id, strategy_instance_id, status);

CREATE INDEX IF NOT EXISTS core_order_quarantine_account_strategy_status_idx
    ON core.core_order_quarantine (account_id, strategy_id, status);

CREATE INDEX IF NOT EXISTS core_order_quarantine_broker_order_status_idx
    ON core.core_order_quarantine (broker_order_id, status);

CREATE INDEX IF NOT EXISTS core_order_quarantine_ib_perm_status_idx
    ON core.core_order_quarantine (ib_perm_id, status);
