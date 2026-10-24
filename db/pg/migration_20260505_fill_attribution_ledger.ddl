CREATE TABLE IF NOT EXISTS core.fill_attribution_ledger (
    attribution_id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_key text NOT NULL,
    account text NULL,
    symbol text NOT NULL,
    asset_class text DEFAULT 'STK'::text NOT NULL,
    broker_order_id text NULL,
    perm_id int8 NULL,
    order_ref text NULL,
    exec_id text NULL,
    side text NULL,
    qty numeric NULL,
    price numeric NULL,
    event_ts timestamptz NULL,
    attribution_status text NOT NULL,
    attribution_reason text NOT NULL,
    matched_core_order_id uuid NULL,
    matched_strategy_id text NULL,
    candidate_count int4 DEFAULT 0 NOT NULL,
    candidate_snapshot jsonb DEFAULT '[]'::jsonb NOT NULL,
    quarantine_id uuid NULL,
    pnl_eligible boolean DEFAULT false NOT NULL,
    ledger_eligible boolean DEFAULT false NOT NULL,
    manual_adjudication_required boolean DEFAULT true NOT NULL,
    adjudicated_by text NULL,
    adjudicated_at timestamptz NULL,
    adjudication_note text NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT fill_attribution_ledger_pkey PRIMARY KEY (attribution_id),
    CONSTRAINT fill_attribution_ledger_event_key_uk UNIQUE (event_key),
    CONSTRAINT fill_attribution_ledger_status_check CHECK (
        attribution_status = ANY (ARRAY[
            'unknown'::text,
            'ambiguous'::text,
            'verified'::text,
            'manual_required'::text,
            'manual_resolved'::text,
            'ignored'::text
        ])
    )
);

CREATE INDEX IF NOT EXISTS fill_attribution_ledger_status_symbol_idx
ON core.fill_attribution_ledger (attribution_status, symbol, updated_at DESC);

CREATE INDEX IF NOT EXISTS fill_attribution_ledger_identity_idx
ON core.fill_attribution_ledger (account, symbol, broker_order_id, perm_id, order_ref, exec_id);

CREATE INDEX IF NOT EXISTS fill_attribution_ledger_adjudication_idx
ON core.fill_attribution_ledger (manual_adjudication_required, adjudicated_at DESC, updated_at DESC);
