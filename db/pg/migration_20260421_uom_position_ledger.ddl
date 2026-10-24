-- UnifiedOrderManager protection registry and append-only position ledger.

CREATE TABLE IF NOT EXISTS core.core_protection_order_registry (
    protection_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_instance_id TEXT,
    symbol TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    position_owner TEXT,
    position_owner_key TEXT,
    bracket_id TEXT,
    order_role TEXT,
    protection_kind TEXT NOT NULL,
    side TEXT NOT NULL,
    qty_bucket TEXT NOT NULL,
    price_band_id TEXT NOT NULL,
    desired_qty NUMERIC NOT NULL,
    desired_price NUMERIC,
    broker_order_id BIGINT,
    ib_perm_id BIGINT,
    order_ref TEXT,
    state TEXT NOT NULL,
    degraded_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_broker_sync_at TIMESTAMPTZ
);

ALTER TABLE IF EXISTS core.core_protection_order_registry
    ADD COLUMN IF NOT EXISTS strategy_instance_id TEXT,
    ADD COLUMN IF NOT EXISTS position_owner TEXT,
    ADD COLUMN IF NOT EXISTS position_owner_key TEXT,
    ADD COLUMN IF NOT EXISTS bracket_id TEXT,
    ADD COLUMN IF NOT EXISTS order_role TEXT;

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_lot
    ON core.core_protection_order_registry(account_id, strategy_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_owner
    ON core.core_protection_order_registry(account_id, strategy_instance_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_owner_key
    ON core.core_protection_order_registry(position_owner_key);

CREATE INDEX IF NOT EXISTS idx_core_protection_registry_state
    ON core.core_protection_order_registry(state);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'core_protection_order_registry_account_known_chk'
          AND conrelid = 'core.core_protection_order_registry'::regclass
    ) THEN
        ALTER TABLE core.core_protection_order_registry
            ADD CONSTRAINT core_protection_order_registry_account_known_chk
            CHECK (btrim(account_id) <> '' AND upper(btrim(account_id)) <> 'UNKNOWN_ACCOUNT')
            NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS core.position_ledger_event (
    ledger_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_instance_id TEXT,
    symbol TEXT NOT NULL,
    lot_id TEXT,
    position_owner_key TEXT,
    bracket_id TEXT,
    order_role TEXT,
    event_type TEXT NOT NULL,
    event_ts TIMESTAMPTZ NOT NULL,
    side TEXT,
    qty_delta NUMERIC NOT NULL DEFAULT 0,
    price NUMERIC,
    commission NUMERIC NOT NULL DEFAULT 0,
    fees NUMERIC NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    broker_order_id BIGINT,
    ib_perm_id BIGINT,
    exec_id TEXT,
    idempotency_key TEXT,
    order_ref TEXT,
    source TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE IF EXISTS core.position_ledger_event
    ADD COLUMN IF NOT EXISTS strategy_instance_id TEXT,
    ADD COLUMN IF NOT EXISTS position_owner_key TEXT,
    ADD COLUMN IF NOT EXISTS bracket_id TEXT,
    ADD COLUMN IF NOT EXISTS order_role TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_position_ledger_exec
    ON core.position_ledger_event(account_id, exec_id)
    WHERE exec_id IS NOT NULL AND exec_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_position_ledger_idempotency
    ON core.position_ledger_event(account_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';

CREATE INDEX IF NOT EXISTS idx_position_ledger_lot
    ON core.position_ledger_event(account_id, strategy_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_position_ledger_owner_lot
    ON core.position_ledger_event(account_id, strategy_instance_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_position_ledger_owner_key
    ON core.position_ledger_event(position_owner_key);

CREATE INDEX IF NOT EXISTS idx_position_ledger_symbol
    ON core.position_ledger_event(account_id, symbol, event_ts);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'position_ledger_event_account_known_chk'
          AND conrelid = 'core.position_ledger_event'::regclass
    ) THEN
        ALTER TABLE core.position_ledger_event
            ADD CONSTRAINT position_ledger_event_account_known_chk
            CHECK (btrim(account_id) <> '' AND upper(btrim(account_id)) <> 'UNKNOWN_ACCOUNT')
            NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS core.position_ledger_current_state (
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_instance_id TEXT,
    symbol TEXT NOT NULL,
    lot_id TEXT,
    position_owner_key TEXT,
    lot_key TEXT NOT NULL DEFAULT '',
    qty NUMERIC NOT NULL DEFAULT 0,
    notional_sum NUMERIC,
    avg_px NUMERIC,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    commission_sum NUMERIC NOT NULL DEFAULT 0,
    fees_sum NUMERIC NOT NULL DEFAULT 0,
    last_event_ts TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy_id, symbol, lot_key)
);

ALTER TABLE IF EXISTS core.position_ledger_current_state
    ADD COLUMN IF NOT EXISTS strategy_instance_id TEXT,
    ADD COLUMN IF NOT EXISTS position_owner_key TEXT;

CREATE INDEX IF NOT EXISTS idx_position_ledger_current_symbol
    ON core.position_ledger_current_state(account_id, symbol);

CREATE INDEX IF NOT EXISTS idx_position_ledger_current_owner_lot
    ON core.position_ledger_current_state(account_id, strategy_instance_id, symbol, lot_id);

CREATE INDEX IF NOT EXISTS idx_position_ledger_current_owner_key
    ON core.position_ledger_current_state(position_owner_key);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'position_ledger_current_state_account_known_chk'
          AND conrelid = 'core.position_ledger_current_state'::regclass
    ) THEN
        ALTER TABLE core.position_ledger_current_state
            ADD CONSTRAINT position_ledger_current_state_account_known_chk
            CHECK (btrim(account_id) <> '' AND upper(btrim(account_id)) <> 'UNKNOWN_ACCOUNT')
            NOT VALID;
    END IF;
END $$;

CREATE OR REPLACE VIEW core.position_ledger_current AS
SELECT
    account_id,
    strategy_id,
    strategy_instance_id,
    symbol,
    lot_id,
    position_owner_key,
    qty,
    CASE
        WHEN ABS(qty) <= 0.000000001 OR avg_px IS NULL THEN NULL
        ELSE ABS(qty) * avg_px
    END AS notional_sum,
    avg_px,
    realized_pnl,
    commission_sum,
    fees_sum,
    last_event_ts
FROM core.position_ledger_current_state;
