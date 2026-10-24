-- IB order truth hardening:
-- 1) extend core.core_order with session identity fields
-- 2) add core.core_order_quarantine for unresolved unknown broker orders

ALTER TABLE core.core_order
    ADD COLUMN IF NOT EXISTS account_id text NULL,
    ADD COLUMN IF NOT EXISTS strategy_instance_id text NULL,
    ADD COLUMN IF NOT EXISTS client_order_id text NULL,
    ADD COLUMN IF NOT EXISTS intent_id text NULL,
    ADD COLUMN IF NOT EXISTS order_role text NULL,
    ADD COLUMN IF NOT EXISTS lot_id text NULL,
    ADD COLUMN IF NOT EXISTS position_owner_key text NULL,
    ADD COLUMN IF NOT EXISTS ib_client_id int4 NULL,
    ADD COLUMN IF NOT EXISTS ib_session_role text NULL,
    ADD COLUMN IF NOT EXISTS ib_connection_epoch int4 NULL,
    ADD COLUMN IF NOT EXISTS source_event_id text NULL;

CREATE INDEX IF NOT EXISTS core_order_ib_client_id_idx
    ON core.core_order (ib_client_id);

CREATE INDEX IF NOT EXISTS core_order_ib_session_role_idx
    ON core.core_order (ib_session_role);

CREATE INDEX IF NOT EXISTS idx_core_order_perm_owner
    ON core.core_order (ib_perm_id, ib_client_id, account_id);

CREATE INDEX IF NOT EXISTS idx_core_order_owner_instance
    ON core.core_order (account_id, strategy_instance_id, symbol);

CREATE INDEX IF NOT EXISTS idx_core_order_client_owner
    ON core.core_order (client_order_id, account_id, strategy_instance_id);

CREATE INDEX IF NOT EXISTS idx_core_order_intent_owner
    ON core.core_order (intent_id, account_id, strategy_instance_id);

CREATE TABLE IF NOT EXISTS core.core_order_quarantine (
    quarantine_id uuid DEFAULT gen_random_uuid() NOT NULL,
    quarantine_scope text NULL,
    account_id text NULL,
    strategy_id text NULL,
    strategy_instance_id text NULL,
    symbol text NOT NULL,
    intent_id text NULL,
    client_order_id text NULL,
    owner_key_hash text NULL,
    order_role text NULL,
    broker_order_id text NULL,
    perm_id int8 NULL,
    ib_perm_id int8 NULL,
    ib_client_id int4 NULL,
    source_event_id text NULL,
    ib_connection_epoch int4 NULL,
    order_ref text NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    note text NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz NULL,
    CONSTRAINT core_order_quarantine_pkey PRIMARY KEY (quarantine_id),
    CONSTRAINT core_order_quarantine_status_check CHECK (
        status = ANY (ARRAY['active'::text, 'resolved'::text, 'cancelled'::text, 'adopted'::text])
    )
);

CREATE INDEX IF NOT EXISTS core_order_quarantine_symbol_status_idx
    ON core.core_order_quarantine (symbol, status);

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

CREATE UNIQUE INDEX IF NOT EXISTS core_order_quarantine_active_broker_order_id_uk
    ON core.core_order_quarantine (broker_order_id)
    WHERE status = 'active' AND broker_order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS core_order_quarantine_active_ib_perm_id_uk
    ON core.core_order_quarantine (ib_perm_id)
    WHERE status = 'active' AND ib_perm_id IS NOT NULL;
